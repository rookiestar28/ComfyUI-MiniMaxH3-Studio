"""Generate the fixed-H3 terminal record fixture and exact portable schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfyui_h3_context.core.fixed_h3_generation import (
    build_fixed_h3_terminal_record,
    validate_fixed_h3_terminal_wire,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_05_fixed_h3_generation.json"
SCHEMA = ROOT / "governance" / "contracts" / "fixed_h3_generation_v1.schema.json"


def _render(value: object, public_pins: tuple[str, ...]) -> str:
    rendered = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    for public_pin in public_pins:
        escaped = "".join(f"\\u{ord(character):04x}" for character in public_pin)
        rendered = rendered.replace(public_pin, escaped)
    return rendered


def build_assets() -> tuple[str, str]:
    """Return deterministic fixture/schema text without executing an H3 path."""

    record = build_fixed_h3_terminal_record()
    wire = record.to_wire()
    validate_fixed_h3_terminal_wire(wire)
    native = wire["native_prerequisites"]
    if type(native) is not dict:
        raise RuntimeError("fixed-H3 native prerequisite projection drifted")
    native_map = cast(dict[str, object], native)
    public_pins = (
        cast(str, wire["official_terminal_disposition_sha256"]),
        cast(str, native_map["host_revision"]),
        cast(str, native_map["source_blob"]),
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "comfyui-h3-context://contracts/fixed_h3_generation_v1.schema.json",
        "title": "Unavailable fixed H3-Base generation evidence v1",
        "description": (
            "Exact terminal record. It contains no generation, score, output, "
            "or comparison evidence."
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
            print("fixed-H3 terminal fixture differs")
            return 1
        if not SCHEMA.is_file() or SCHEMA.read_text(encoding="utf-8") != schema_text:
            print("fixed-H3 terminal schema differs")
            return 1
        print("fixed-H3 terminal fixture and schema are byte-identical")
        return 0
    FIXTURE.write_text(fixture_text, encoding="utf-8", newline="\n")
    SCHEMA.write_text(schema_text, encoding="utf-8", newline="\n")
    print(FIXTURE.relative_to(ROOT).as_posix())
    print(SCHEMA.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
