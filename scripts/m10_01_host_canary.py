"""Offline M10-01 public-host-seam canary report.

The script intentionally does not start ComfyUI or import a frontend. It records the current
contract result as unsupported until a separately authorized pinned-host seam run exists.
"""

from __future__ import annotations

import argparse
import json

from comfyui_h3_context.core.capability_manifest import (
    HostCanaryOutcome,
    HostCanarySeam,
    build_host_canary_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the machine-readable report")
    args = parser.parse_args()
    report = build_host_canary_report(
        "supported-host-canary-not-run",
        {
            seam: (HostCanaryOutcome.UNSUPPORTED, "supported_host seam evidence is not available")
            for seam in HostCanarySeam
        },
    )
    payload = report.to_wire()
    if args.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(
            f"status={payload['status']} schema={payload['schema']} "
            f"fingerprint={payload['fingerprint']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
