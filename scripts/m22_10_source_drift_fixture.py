"""Generate or verify the current M22-10 source-drift v2 fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# IMPORTANT: run against this worktree, not an editable install owned by another worktree.
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "m22_10_source_drift_checkpoint.json"


def _render(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def run(*, check: bool) -> int:
    from comfyui_h3_context.core.source_drift_checkpoint import (
        build_default_source_drift_checkpoint,
    )

    expected = _render(build_default_source_drift_checkpoint().to_wire())
    if check:
        if not FIXTURE.is_file() or FIXTURE.read_text(encoding="utf-8") != expected:
            print("M22-10 source drift fixture: DRIFT")
            return 1
        print("M22-10 source drift fixture: PASS")
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(expected, encoding="utf-8", newline="\n")
    print("M22-10 source drift fixture: written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return run(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
