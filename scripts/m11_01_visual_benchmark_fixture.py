"""Offline M11-01 visual benchmark/adapter-selection fixture.

The fixture freezes and checks the benchmark envelope only.  It does not load visual models,
decode media, start ComfyUI, contact Ollama, or produce perception results.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping

from comfyui_h3_context.core import (
    VisualBenchmarkPlan,
    VisualCapability,
    VisualDisposition,
    build_default_visual_benchmark_plan,
)


def _coverage(plan: VisualBenchmarkPlan) -> list[str]:
    return sorted(
        {capability.value for fixture in plan.fixtures for capability in fixture.capabilities}
    )


def _metric_coverage(plan: VisualBenchmarkPlan) -> dict[str, int]:
    return {
        capability.value: sum(metric.capability is capability for metric in plan.metrics)
        for capability in VisualCapability
    }


def _assert_redacted(value: object) -> None:
    """Reject accidental locator/content material from the public fixture projection."""

    if isinstance(value, str):
        lowered = value.casefold()
        forbidden = ("http://", "https://", "file://", "token=", "authorization", "password")
        if any(marker in lowered for marker in forbidden):
            raise AssertionError("fixture projection contains sensitive locator material")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_redacted(key)
            _assert_redacted(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_redacted(item)


def run() -> dict[str, object]:
    plan = build_default_visual_benchmark_plan()
    repeat = build_default_visual_benchmark_plan()
    if plan.fingerprint != repeat.fingerprint:
        raise AssertionError("frozen visual benchmark fingerprint is not deterministic")
    if plan.executable_candidate_ids:
        raise AssertionError("offline M11-01 fixture cannot qualify a live profile")
    if set(_coverage(plan)) != {capability.value for capability in VisualCapability}:
        raise AssertionError("visual capability coverage is incomplete")
    if any(candidate.disposition not in set(VisualDisposition) for candidate in plan.candidates):
        raise AssertionError("candidate disposition is not explicit")
    summary = plan.to_public_summary()
    summary.update(
        {
            "fixture_capabilities": _coverage(plan),
            "metric_coverage": _metric_coverage(plan),
            "candidate_dispositions": {
                candidate.candidate_id: candidate.disposition.value for candidate in plan.candidates
            },
            "native_live_qualification": "not_run",
            "ollama_live_qualification": "not_run",
            "specialist_live_qualification": "not_run",
            "media_decode": "not_run",
            "network_contact": "not_run",
        }
    )
    _assert_redacted(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one redacted JSON object")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print(
            "M11-01 offline benchmark plan "
            f"{result['plan_id']} ({result['plan_fingerprint']}); "
            f"{result['candidate_count']} candidates, "
            f"{result['fixture_count']} fixtures, structural-only"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
