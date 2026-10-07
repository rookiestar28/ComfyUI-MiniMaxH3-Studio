"""Run the bounded, offline M15 performance qualification gate."""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import platform
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: direct execution must measure this exact worktree, not an installed copy.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core import (  # noqa: E402
    BenchmarkOutput,
    ExecutionCorrelation,
    LocalResourceBudget,
    PerformanceSampleMetrics,
    PerformanceSampleObserver,
    PerformanceWorkload,
    QualificationStatus,
    ResourceCacheKey,
    ResourceCancellationToken,
    ResourceExecutionArtifact,
    ResourceExecutionContext,
    ResourceExecutionRequest,
    ResourceExecutionResult,
    ResourceExecutionStatus,
    ResourceScheduler,
    TaskMode,
    build_native_h3_wiring,
    build_product_shell_projection,
    canonical_bytes,
    canonical_fingerprint,
    default_performance_profiles,
    run_performance_benchmark,
    unavailable_performance_qualification,
)
from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    prepare_regular_output,
)
from comfyui_h3_context.nodes import (  # noqa: E402
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

PERFORMANCE_GATE_SCHEMA = "h3.performance.gate.v1"
MAX_PERFORMANCE_REPORT_BYTES = 131_072
_PROMPT = "Preserve the exact package benchmark intent."
_WORKLOAD_FINGERPRINT = canonical_fingerprint(
    {
        "schema": "h3.performance.workload.v1",
        "workload_id": "deterministic_product_pipeline",
        "mode": "T2VA",
        "duration_seconds": 5,
    }
)
_CONFIG_FINGERPRINT = canonical_fingerprint(
    {"schema": "h3.performance.config.v1", "cache_ttl_seconds": 60, "concurrency": 1}
)
_GPU_WORKLOAD_FINGERPRINT = canonical_fingerprint(
    {"schema": "h3.performance.workload.v1", "workload_id": "supported_host_model_execution"}
)


class PerformanceGateError(RuntimeError):
    """Raised when the offline gate cannot produce bounded, truthful evidence."""


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def current_process_rss_bytes() -> int | None:
    """Return content-free current/max RSS using only the Python standard library."""

    if os.name == "nt":
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            # CRITICAL: Linux Python stubs omit Windows-only ctypes symbols; this branch is
            # reached only when the RSS probe is explicitly running on Windows.
            win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
            kernel32 = win_dll("kernel32", use_last_error=True)
            psapi = win_dll("psapi", use_last_error=True)
            # IMPORTANT: ctypes defaults HANDLE returns to 32-bit int; on 64-bit Windows that
            # turns the pseudo-handle into an invalid value and silently drops RSS evidence.
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = (
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            )
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            success = psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            )
            return int(counters.working_set_size) if success else None
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    try:
        resource_module = cast(Any, importlib.import_module("resource"))
        maximum = int(resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss)
        return maximum if sys.platform == "darwin" else maximum * 1024
    except (ImportError, OSError, TypeError, ValueError):
        return None


class LocalProcessSampleObserver:
    """Measure monotonic latency, traced Python peak, and process-owned RSS."""

    def observe(
        self, execute: Callable[[], ResourceExecutionResult]
    ) -> tuple[ResourceExecutionResult, PerformanceSampleMetrics]:
        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        tracemalloc.reset_peak()
        before_rss = current_process_rss_bytes()
        started = time.perf_counter_ns()
        try:
            result = execute()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            _current, python_peak = tracemalloc.get_traced_memory()
            after_rss = current_process_rss_bytes()
        finally:
            if not already_tracing and tracemalloc.is_tracing():
                tracemalloc.stop()
        rss_values = [value for value in (before_rss, after_rss) if value is not None]
        return result, PerformanceSampleMetrics(
            latency_ms=elapsed_ms,
            python_peak_bytes=python_peak,
            process_rss_peak_bytes=max(rss_values) if rss_values else None,
        )


def _benchmark_output() -> BenchmarkOutput:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        _PROMPT,
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _prompt, _report, document = H3ContextCompilerNode().compile(plan)
    validated = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = build_native_h3_wiring(validated)
    projection = build_product_shell_projection(
        validated,
        wiring,
        ExecutionCorrelation("performance-probe", "1"),
    )
    wire = projection.to_wire()
    encoded = canonical_bytes(wire)
    return BenchmarkOutput(
        output_fingerprint=canonical_fingerprint(wire),
        output_bytes=len(encoded),
        output_items=len(projection.field_ids) + len(projection.bindings),
    )


def _workload() -> PerformanceWorkload:
    preflight = _benchmark_output()
    return PerformanceWorkload(
        workload_id="deterministic_product_pipeline",
        workload_fingerprint=_WORKLOAD_FINGERPRINT,
        config_fingerprint=_CONFIG_FINGERPRINT,
        expected_output_bytes=preflight.output_bytes,
        expected_output_items=preflight.output_items,
        execute=lambda _context: _benchmark_output(),
    )


def _control_request(
    *, wall_seconds: float = 1.0, output_bytes: int = 8
) -> ResourceExecutionRequest:
    return ResourceExecutionRequest(
        cache_key=ResourceCacheKey(
            operation_id="performance.control",
            contract_schema="h3.performance.control.v1",
            provider_id="package.performance",
            provider_version="control.v1",
            settings_fingerprint=_CONFIG_FINGERPRINT,
            source_fingerprints=(_WORKLOAD_FINGERPRINT,),
        ),
        budget=LocalResourceBudget(1024, wall_seconds, 0, output_bytes, 2, 1),
        cache_enabled=False,
        cache_ttl_seconds=0.0,
        cancellation_required=True,
    )


def _run_control_flow(*, max_cancellation_latency_ms: float) -> dict[str, object]:
    pre_scheduler = ResourceScheduler()
    pre_token = ResourceCancellationToken()
    pre_token.cancel("benchmark stopped")
    pre = pre_scheduler.execute(
        _control_request(),
        lambda _context: ResourceExecutionArtifact(value="must-not-run"),
        cancellation_probe=pre_token,
    )

    mid_scheduler = ResourceScheduler()
    mid_token = ResourceCancellationToken()
    mid_started = time.perf_counter_ns()

    def cancel_during(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
        mid_token.cancel("benchmark checkpoint")
        context.checkpoint()
        return ResourceExecutionArtifact(value="must-not-complete")

    mid = mid_scheduler.execute(_control_request(), cancel_during, cancellation_probe=mid_token)
    mid_latency_ms = (time.perf_counter_ns() - mid_started) / 1_000_000

    timeout_now = 0.0

    def timeout_worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
        nonlocal timeout_now
        timeout_now = 2.0
        context.checkpoint()
        return ResourceExecutionArtifact(value="must-not-complete")

    timeout_scheduler = ResourceScheduler(clock=lambda: timeout_now)
    timeout = timeout_scheduler.execute(
        _control_request(wall_seconds=1.0),
        timeout_worker,
        cancellation_probe=ResourceCancellationToken(),
    )

    ceiling_scheduler = ResourceScheduler()

    def ceiling_worker(context: ResourceExecutionContext) -> ResourceExecutionArtifact:
        context.record_output(byte_count=9)
        return ResourceExecutionArtifact(value="must-not-complete", output_bytes=9)

    ceiling = ceiling_scheduler.execute(
        _control_request(output_bytes=8),
        ceiling_worker,
        cancellation_probe=ResourceCancellationToken(),
    )
    states: dict[str, object] = {
        "pre_cancel": "cancelled_before_worker"
        if pre.status is ResourceExecutionStatus.CANCELLED
        and not pre.receipt.worker_started
        and pre_scheduler.active == 0
        else "failed",
        "mid_run_cancel": "cancelled_and_released"
        if mid.status is ResourceExecutionStatus.CANCELLED
        and mid.receipt.reservation_released
        and mid_scheduler.active == 0
        and mid_scheduler.cache_size == 0
        else "failed",
        "mid_run_cancellation_latency_ms": mid_latency_ms,
        "mid_run_cancellation_ceiling": "within_profile_budget"
        if mid_latency_ms <= max_cancellation_latency_ms
        else "failed",
        "timeout": "timed_out_and_released"
        if timeout.status is ResourceExecutionStatus.TIMED_OUT
        and timeout.receipt.reservation_released
        and timeout_scheduler.active == 0
        else "failed",
        "output_ceiling": "rejected_and_released"
        if ceiling.status is ResourceExecutionStatus.BUDGET_EXCEEDED
        and ceiling.receipt.reservation_released
        and ceiling_scheduler.active == 0
        else "failed",
    }
    return states


def build_performance_report(
    candidate_commit: str,
    *,
    observer: PerformanceSampleObserver | None = None,
) -> dict[str, object]:
    """Build one content-free model-free report for the exact candidate."""

    selected_observer = LocalProcessSampleObserver() if observer is None else observer
    workload = _workload()
    profiles = default_performance_profiles()
    results = [
        run_performance_benchmark(
            profile,
            workload,
            candidate_commit=candidate_commit,
            observer=selected_observer,
        )
        for profile in profiles[:2]
    ]
    results.extend(
        unavailable_performance_qualification(
            profile,
            candidate_commit=candidate_commit,
            workload_fingerprint=_GPU_WORKLOAD_FINGERPRINT,
            config_fingerprint=_CONFIG_FINGERPRINT,
            diagnostic_code="model_gpu_execution_not_authorized",
        )
        for profile in profiles[2:]
    )
    package_cancellation_ceiling_ms = min(
        profile.budget.max_cancellation_latency_ms for profile in profiles[:2]
    )
    control_flow = _run_control_flow(max_cancellation_latency_ms=package_cancellation_ceiling_ms)
    expected_unavailable = all(
        result.status is QualificationStatus.UNAVAILABLE for result in results[2:]
    )
    package_qualified = all(
        result.status is QualificationStatus.QUALIFIED for result in results[:2]
    )
    controls_pass = all(
        value != "failed"
        for key, value in control_flow.items()
        if key != "mid_run_cancellation_latency_ms"
    )
    status = "PASS" if package_qualified and expected_unavailable and controls_pass else "FAIL"
    report: dict[str, object] = {
        "schema": PERFORMANCE_GATE_SCHEMA,
        "status": status,
        "candidate_commit": candidate_commit,
        "environment": {
            "implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
        },
        "network": "disabled",
        "model_execution": "not_run",
        "profiles": [result.to_wire() for result in results],
        "control_flow": control_flow,
    }
    encoded = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_PERFORMANCE_REPORT_BYTES:
        raise PerformanceGateError("performance report exceeds its byte bound")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_performance_report(args.candidate)
        output = prepare_regular_output(args.report)
        output.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (OSError, PerformanceGateError, UnsafePathError, ValueError) as exc:
        print(f"PERFORMANCE GATE: FAIL: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
