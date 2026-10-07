"""Emit and verify the versioned ManagedRun stategraph contract.

The artifact is projected from `MANAGED_RUN_MACHINE` rather than written beside it. A
hand-maintained copy would be a second source of truth that drifts silently, and the whole point of
publishing the graph is that a reader can trust it describes the transitions the code actually
takes -- including which guard each one must satisfy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfyui_h3_context.core.managed_run import (  # noqa: E402
    MANAGED_RUN_MACHINE,
    managed_run_stategraph,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = "governance/contracts/managed_run_stategraph_v1.json"


def artifact_bytes() -> bytes:
    graph = managed_run_stategraph()
    return (json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    expected = artifact_bytes()
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check:
        current = target.read_bytes() if target.is_file() else b""
        if current != expected:
            print(
                json.dumps(
                    {"status": "FAIL", "detail": "managed run stategraph artifact is stale"},
                    ensure_ascii=False,
                )
            )
            return 1
    print(
        json.dumps(
            {
                "schema": "h3-context-managed-run-stategraph-report/1",
                "states": len(MANAGED_RUN_MACHINE.states),
                "status": "PASS",
                "transitions": len(json.loads(expected)["transitions"]),
                "triggers": len(MANAGED_RUN_MACHINE.triggers),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
