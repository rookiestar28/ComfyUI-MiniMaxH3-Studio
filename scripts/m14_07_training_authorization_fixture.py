"""Generate or verify the deterministic training-authorization terminal assets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from comfyui_h3_context.core.training_authorization import (
    build_training_authorization_terminal_record,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_07_training_authorization.json"
SCHEMA = ROOT / "governance" / "contracts" / "training_authorization_v1.schema.json"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _assets() -> dict[Path, bytes]:
    wire = build_training_authorization_terminal_record().to_wire()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "H3 training authorization terminal decision",
        "description": (
            "Closed, content-free DO_NOT_IMPLEMENT decision. Production validation also "
            "regenerates predecessor and semantic authority."
        ),
        "type": "object",
        "const": wire,
    }
    return {FIXTURE: _json_bytes(wire), SCHEMA: _json_bytes(schema)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = _assets()
    if args.check:
        drifted = [
            str(path.relative_to(ROOT))
            for path, content in expected.items()
            if not path.is_file() or path.read_bytes() != content
        ]
        if drifted:
            print("training authorization assets drifted: " + ", ".join(drifted))
            return 1
        print("training authorization assets: PASS")
        return 0
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
