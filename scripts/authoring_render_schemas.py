"""Derive tooling-only closed Authoring render wire schemas from their finite core fields."""

from __future__ import annotations

import argparse
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from comfyui_h3_context.core.authoring_render_jobs import (
    RENDER_JOB_REQUEST_SCHEMA,
    AuthoringRenderJobRequestV1,
)
from comfyui_h3_context.core.authoring_render_receipts import (
    RENDER_OUTPUT_FACTS_SCHEMA,
    RENDER_RECEIPT_SCHEMA,
    AuthoringRenderReceiptV1,
    MeasuredRenderOutput,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "governance" / "contracts"
# CRITICAL: JSON Schema's dollar anchor accepts a final newline; the negative lookahead
# preserves the core fullmatch boundary instead of admitting a different wire vocabulary.
FINGERPRINT = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$(?![\s\S])", "maxLength": 71}
TOKEN = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_]{0,31}$(?![\s\S])", "maxLength": 32}
INTEGER = {"type": "integer", "minimum": 0, "maximum": 1_000_000_000}


def _closed(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def render_wire_schemas() -> dict[str, dict[str, Any]]:
    request_fields: dict[str, Any] = {}
    for field in fields(AuthoringRenderJobRequestV1):
        if field.name.endswith("_fingerprint"):
            value = FINGERPRINT
        elif field.name == "schema":
            value = {"const": RENDER_JOB_REQUEST_SCHEMA}
        elif field.name == "workspace_handle":
            value = {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$(?![\s\S])",
                "maxLength": 128,
            }
        elif field.name == "idempotency_key":
            value = {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$(?![\s\S])",
                "maxLength": 128,
            }
        elif field.name == "timeout_ms":
            value = {"type": "integer", "minimum": 1, "maximum": 900_000}
        elif field.name in {"workspace_revision", "timeline_revision"}:
            value = {"type": "integer", "minimum": 0, "maximum": 1_000_000}
        else:
            raise ValueError("unclassified render request field")
        request_fields[field.name] = value
    request = _closed(request_fields)
    observation_fields: dict[str, Any] = {"schema": {"const": RENDER_OUTPUT_FACTS_SCHEMA}}
    tokens = {
        "container",
        "video_codec",
        "pixel_format",
        "color_range",
        "color_space",
        "color_primaries",
        "color_transfer",
        "audio_codec",
    }
    nullable = {"audio_codec", "audio_sample_rate", "audio_channels", "audio_effective_samples"}
    for field in fields(MeasuredRenderOutput):
        if field.name.endswith("fingerprint"):
            value = FINGERPRINT
        elif field.name in tokens:
            value = TOKEN
        else:
            value = {**INTEGER, "minimum": 1} if field.name == "byte_length" else INTEGER
        if field.name in nullable:
            value = {"anyOf": [value, {"type": "null"}]}
        observation_fields[field.name] = value
    receipt_fields: dict[str, Any] = {}
    for field in fields(AuthoringRenderReceiptV1):
        if field.name.endswith("fingerprint"):
            value = FINGERPRINT
        elif field.name == "schema":
            value = {"const": RENDER_RECEIPT_SCHEMA}
        elif field.name == "request":
            value = request
        elif field.name == "observed":
            value = _closed(observation_fields)
        else:
            raise ValueError("unclassified render receipt field")
        receipt_fields[field.name] = value
    schemas = {
        "authoring_render_job_request_v1.schema.json": request,
        "authoring_render_receipt_v1.schema.json": _closed(receipt_fields),
    }
    return {
        name: {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "comfyui-h3-context://contracts/" + name,
            **schema,
        }
        for name, schema in schemas.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, wire in render_wire_schemas().items():
        encoded = json.dumps(wire, ensure_ascii=False, indent=2) + "\n"
        path = SCHEMA_ROOT / name
        if args.write:
            path.write_text(encoded, encoding="utf-8", newline="\n")
        if args.check or not args.write:
            if not path.is_file() or path.read_text(encoding="utf-8") != encoded:
                print("render wire schema differs: " + name)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
