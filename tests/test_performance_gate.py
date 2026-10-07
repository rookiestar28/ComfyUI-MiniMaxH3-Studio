from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import PerformanceSampleMetrics, ResourceExecutionResult
from scripts.performance_gate import (
    MAX_PERFORMANCE_REPORT_BYTES,
    build_performance_report,
    current_process_rss_bytes,
)

ROOT = Path(__file__).parents[1]
COMMIT = "1" * 40
PROMPT_SENTINEL = "Preserve the exact package benchmark intent."


class _Observer:
    def __init__(self) -> None:
        self.calls = 0

    def observe(
        self, execute: Callable[[], ResourceExecutionResult]
    ) -> tuple[ResourceExecutionResult, PerformanceSampleMetrics]:
        self.calls += 1
        return execute(), PerformanceSampleMetrics(
            latency_ms=float(self.calls),
            python_peak_bytes=16_384,
            process_rss_peak_bytes=32_768,
        )


def test_model_free_report_qualifies_package_profiles_and_abstains_from_gpu() -> None:
    observer = _Observer()
    report = build_performance_report(COMMIT, observer=observer)
    assert report["schema"] == "h3.performance.gate.v1"
    assert report["status"] == "PASS"
    assert report["candidate_commit"] == COMMIT
    assert report["network"] == "disabled"
    assert report["model_execution"] == "not_run"
    assert observer.calls == 10  # CPU 1+5 and low-resource 1+3

    profiles = cast(list[dict[str, object]], report["profiles"])
    assert isinstance(profiles, list)
    assert [item["profile_id"] for item in profiles] == [
        "cpu",
        "low_resource",
        "mainstream_gpu",
        "high_fidelity",
    ]
    assert [item["status"] for item in profiles] == [
        "qualified",
        "qualified",
        "unavailable",
        "unavailable",
    ]
    assert profiles[2]["peak_vram_bytes"] is None
    assert profiles[3]["peak_vram_bytes"] is None
    control_flow = cast(dict[str, object], report["control_flow"])
    assert control_flow["pre_cancel"] == "cancelled_before_worker"
    assert control_flow["mid_run_cancel"] == "cancelled_and_released"
    assert control_flow["mid_run_cancellation_ceiling"] == "within_profile_budget"
    assert control_flow["timeout"] == "timed_out_and_released"
    assert control_flow["output_ceiling"] == "rejected_and_released"

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    assert len(encoded) <= MAX_PERFORMANCE_REPORT_BYTES
    assert PROMPT_SENTINEL not in encoded.decode()
    assert str(ROOT) not in encoded.decode()


def test_report_fails_when_real_cancellation_latency_exceeds_package_profiles(
    monkeypatch: object,
) -> None:
    clock = iter((0, 200_000_000))
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "scripts.performance_gate.time.perf_counter_ns",
        lambda: next(clock),
    )
    report = build_performance_report(COMMIT, observer=_Observer())
    assert report["status"] == "FAIL"
    control_flow = cast(dict[str, object], report["control_flow"])
    assert control_flow["mid_run_cancellation_latency_ms"] == 200.0
    assert control_flow["mid_run_cancellation_ceiling"] == "failed"


def test_report_matches_strict_shipped_schema_and_process_observer_is_optional() -> None:
    report = build_performance_report(COMMIT, observer=_Observer())
    schema = json.loads(
        (ROOT / "governance/contracts/performance_gate_v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(report)) == []
    rss = current_process_rss_bytes()
    assert rss is None or rss > 0
