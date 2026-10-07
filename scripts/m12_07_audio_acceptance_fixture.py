"""Offline M12-07 audio-perception acceptance gate fixture.

The fixture evaluates frozen M12 metadata only.  It intentionally returns a bounded
``unsupported_research`` result because no live audio profile was admitted or qualified.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping

from comfyui_h3_context.core import (
    AudioAcceptanceRuntime,
    AudioAcceptanceStatus,
    build_default_audio_benchmark_plan,
    evaluate_audio_acceptance,
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
    plan = build_default_audio_benchmark_plan()
    report = evaluate_audio_acceptance(
        plan,
        runtime=AudioAcceptanceRuntime(
            limitations=(
                "no live audio profile was admitted; core/manual workflows remain usable",
                "audio accuracy, resource, and host qualification remain unavailable",
            )
        ),
    )
    if report.status is not AudioAcceptanceStatus.UNSUPPORTED_RESEARCH:
        raise AssertionError("offline M12-07 fixture must remain unsupported research")
    if report.executable_candidate_ids:
        raise AssertionError("offline M12-07 fixture cannot produce executable candidates")
    summary = report.to_public_dict()
    summary.update(
        {
            "benchmark_plan_fingerprint": plan.fingerprint,
            "case_count": len(plan.fixtures),
            "capability_count": len(
                {capability for fixture in plan.fixtures for capability in fixture.capabilities}
            ),
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
            "M12-07 audio acceptance fixture: "
            f"{result['status']} ({result['case_count']} cases; no live execution)"
        )


if __name__ == "__main__":
    main()
