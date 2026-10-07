"""Emit the bounded M14-04 current-environment qualification disposition."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfyui_h3_context.core.local_qualification import (
    CandidateDisposition,
    ProductScopeDisposition,
    build_default_local_qualification_plan,
    evaluate_local_qualification,
)


def run() -> dict[str, object]:
    """Evaluate the preregistered all-unavailable matrix without executing a backend."""

    plan = build_default_local_qualification_plan()
    report = evaluate_local_qualification(plan)
    if report.product_scope is not ProductScopeDisposition.MANUAL_ONLY_SCOPED:
        raise RuntimeError("unexecuted default candidates cannot produce an assisted disposition")
    if any(
        receipt.disposition is not CandidateDisposition.UNAVAILABLE
        for receipt in report.candidate_receipts
    ):
        raise RuntimeError("default candidate availability drifted")
    result = report.to_wire()
    result["report_fingerprint"] = report.fingerprint
    result["network_contacted"] = False
    result["host_started"] = False
    result["media_started"] = False
    result["reference_code_executed"] = False
    result["provider_selected"] = False
    return result


def main() -> int:
    print(json.dumps(run(), indent=2, sort_keys=True))
    print("M14-04 local qualification fixture: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
