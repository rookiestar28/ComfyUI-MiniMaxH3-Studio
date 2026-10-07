"""Generate the unavailable human-review fixture and exact portable schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfyui_h3_context.core.human_review import (
    build_human_review_terminal_record,
    validate_human_review_terminal_wire,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_06_human_review.json"
SCHEMA = ROOT / "governance" / "contracts" / "human_review_v1.schema.json"


def _render(value: object, public_pins: tuple[str, ...]) -> str:
    rendered = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    for public_pin in public_pins:
        escaped = "".join(f"\\u{ord(character):04x}" for character in public_pin)
        rendered = rendered.replace(public_pin, escaped)
    return rendered


def build_assets() -> tuple[str, str]:
    """Return deterministic assets without recruiting reviewers or accessing external systems."""

    record = build_human_review_terminal_record()
    wire = record.to_wire()
    validate_human_review_terminal_wire(wire)
    public_pins = (record.fixed_h3_terminal_fingerprint,)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "comfyui-h3-context://contracts/human_review_v1.schema.json",
        "title": "Unavailable governed human-review evidence v1",
        "description": (
            "Exact terminal record. It contains no reviewer, response, PII, preference, "
            "comparison, or promotion evidence. Draft 2020-12 integral decimal forms are "
            "parsed with exact bounded decimal semantics and canonicalized by the production "
            "validator to the module-owned integer record."
        ),
        "type": "object",
        "minProperties": len(wire),
        "maxProperties": len(wire),
        "required": sorted(wire),
        "properties": {key: {} for key in sorted(wire)},
        "additionalProperties": False,
        "const": wire,
    }
    return _render(wire, public_pins), _render(schema, public_pins)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    fixture_text, schema_text = build_assets()
    if args.check:
        if not FIXTURE.is_file() or FIXTURE.read_text(encoding="utf-8") != fixture_text:
            print("human-review terminal fixture differs")
            return 1
        if not SCHEMA.is_file() or SCHEMA.read_text(encoding="utf-8") != schema_text:
            print("human-review terminal schema differs")
            return 1
        print("human-review terminal fixture and schema are byte-identical")
        return 0
    FIXTURE.write_text(fixture_text, encoding="utf-8", newline="\n")
    SCHEMA.write_text(schema_text, encoding="utf-8", newline="\n")
    print(FIXTURE.relative_to(ROOT).as_posix())
    print(SCHEMA.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
