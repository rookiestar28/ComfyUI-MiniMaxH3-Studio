"""Offline M11-08 visual-perception acceptance gate fixture.

The fixture evaluates the frozen M11-01 metadata only.  It intentionally returns a bounded
``unsupported_research`` result because no live visual profile was admitted or qualified.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping

from comfyui_h3_context.core import (
    AcceptanceStatus,
    VisualAcceptanceRuntime,
    build_default_visual_benchmark_plan,
    evaluate_visual_acceptance,
)


def _assert_redacted(value: object) -> None:
    if isinstance(value, str):
        lowered = value.casefold()
        forbidden = (
            "http://",
            "https://",
            "file://",
            "/mnt/",
            "token=",
            "authorization",
            "password",
        )
        if any(marker in lowered for marker in forbidden):
            raise AssertionError(
                "acceptance fixture projection contains sensitive locator material"
            )
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
    report = evaluate_visual_acceptance(
        plan,
        runtime=VisualAcceptanceRuntime(
            limitations=(
                "no live visual profile was admitted; core/manual workflows remain usable",
                "accuracy, calibration, latency, memory, and host qualification remain unavailable",
            )
        ),
    )
    if report.status is not AcceptanceStatus.UNSUPPORTED_RESEARCH:
        raise AssertionError("offline M11-08 fixture must remain unsupported research")
    if report.executable_candidate_ids:
        raise AssertionError("offline M11-08 fixture cannot produce executable candidates")
    summary = report.to_public_dict()
    summary.update(
        {
            "benchmark_plan_fingerprint": plan.fingerprint,
            "candidate_count": len(plan.candidates),
            "capability_count": len(plan.capability_policy.mandatory)
            + len(plan.capability_policy.optional),
            "native_live_execution": "not_started",
            "ollama_live_execution": "not_started",
            "specialist_live_execution": "not_started",
            "media_decode": "not_started",
            "network_contact": "not_started",
            "host_runtime": "not_started",
            "optional_dependencies_absent": True,
        }
    )
    _assert_redacted(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the redacted fixture summary")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print(
            "M11-08 visual acceptance fixture: "
            f"{result['status']} ({result['candidate_count']} candidates; no live execution)"
        )


if __name__ == "__main__":
    main()
