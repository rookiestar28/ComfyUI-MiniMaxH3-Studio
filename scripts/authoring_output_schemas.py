"""Derive tooling-only Authoring transport schemas; runtime codecs remain authoritative."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from comfyui_h3_context.adapters.comfyui_authoring_output_runtime import AuthoringOutputRuntime
from comfyui_h3_context.core import authoring_output_protocol as protocol
from comfyui_h3_context.core.authoring_render_jobs import RenderJobFailure, RenderJobPhase
from comfyui_h3_context.core.composition_contract import OUTPUT_PROFILE_ID

ROOT = Path(__file__).resolve().parents[1]
NAME = "authoring_output_v1.schema.json"


def _closed(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _text(pattern: str, maximum: int) -> dict[str, Any]:
    # SECURITY: unlike dollar alone, this end assertion also rejects a trailing newline.
    return {"type": "string", "pattern": "^" + pattern + r"(?![\s\S])", "maxLength": maximum}


def _integer(maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def output_wire_schema() -> dict[str, Any]:
    fingerprint = _text(r"sha256:[0-9a-f]{64}", 71)
    workspace = _text(r"authoring-[0-9a-f]{32}", 42)
    binding = {
        "workspace_handle": workspace,
        "workspace_revision": _integer(1_000_000),
        "timeline_revision": _integer(1_000_000),
        "snapshot_fingerprint": fingerprint,
    }
    create = _closed(
        {
            "schema": {"const": protocol.OUTPUT_CREATE_SCHEMA},
            **binding,
            "output_profile_id": {"const": OUTPUT_PROFILE_ID},
            "idempotency_key": _text(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}", 128),
        }
    )
    cancel = _closed(
        {"schema": {"const": protocol.OUTPUT_CANCEL_SCHEMA}, "workspace_handle": workspace}
    )
    summary = _closed(
        {
            "output_fingerprint": fingerprint,
            "byte_length": _integer(protocol.OUTPUT_MAX_BYTES, 1),
            "width": {**_integer(1920, 2), "multipleOf": 2},
            "height": {**_integer(1080, 2), "multipleOf": 2},
            "frame_count": _integer(protocol.OUTPUT_MAX_FRAMES, 1),
            "frame_rate_num": {"type": "integer", "const": 24},
            "frame_rate_den": {"type": "integer", "const": 1},
            "audio_streams": {"type": "integer", "enum": [0, 1]},
            "output_profile_id": {"const": OUTPUT_PROFILE_ID},
            "verified": {"const": True},
        }
    )
    status = _closed(
        {
            "schema": {"const": protocol.OUTPUT_STATUS_SCHEMA},
            **binding,
            "job_handle": _text(r"arj_[A-Za-z0-9_-]{22}", 26),
            "output_handle": {"anyOf": [_text(r"aro_[A-Za-z0-9_-]{22}", 26), {"type": "null"}]},
            "state_version": _integer(1_000_000),
            "phase": {"enum": [p.value for p in RenderJobPhase]},
            "progress_bp": _integer(10_000),
            "failure": {"enum": [None, *[f.value for f in RenderJobFailure]]},
            "currency": {"enum": ["current", "old_revision"]},
            "availability": {"enum": ["available", "gone", "expired"]},
            "output": {"anyOf": [{"$ref": "#/$defs/summary"}, {"type": "null"}]},
        }
    )
    status["allOf"] = [
        {
            "if": {"properties": {"phase": {"const": "succeeded"}}},
            "then": {"properties": {"failure": {"type": "null"}, "progress_bp": {"const": 10000}}},
            "else": {
                "properties": {
                    "output_handle": {"type": "null"},
                    "output": {"type": "null"},
                    "availability": {"enum": ["gone", "expired"]},
                }
            },
        },
        {
            "if": {"properties": {"phase": {"const": "failed"}}},
            "then": {
                "properties": {
                    "failure": {
                        "enum": [f.value for f in RenderJobFailure if f.value != "cancelled"]
                    }
                }
            },
        },
        {
            "if": {"properties": {"phase": {"const": "cancelled"}}},
            "then": {"properties": {"failure": {"const": "cancelled"}}},
        },
        {
            "if": {
                "properties": {"phase": {"not": {"enum": ["failed", "cancelled", "succeeded"]}}}
            },
            "then": {"properties": {"failure": {"type": "null"}, "progress_bp": {"maximum": 9999}}},
        },
        {
            "if": {"properties": {"output_handle": {"type": "null"}}},
            "then": {"properties": {"output": {"type": "null"}}},
            "else": {"properties": {"output": {"$ref": "#/$defs/summary"}}},
        },
        {
            "if": {"properties": {"availability": {"const": "available"}}},
            "then": {
                "properties": {
                    "phase": {"const": "succeeded"},
                    "output": {"$ref": "#/$defs/summary"},
                }
            },
        },
    ]
    owner = AuthoringOutputRuntime(None)
    try:
        capability = _closed(
            {
                key: ({"type": "boolean"} if key == "supported" else {"const": value})
                for key, value in owner.capability().items()
            }
        )
    finally:
        owner.close()
    error = _closed(
        {
            "schema": {"const": protocol.OUTPUT_ERROR_SCHEMA},
            "code": {"enum": list(protocol._ERROR_STATUSES)},
        }
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "comfyui-h3-context://contracts/" + NAME,
        "$defs": {
            "create": create,
            "cancel": cancel,
            "summary": summary,
            "status": status,
            "capability": capability,
            "error": error,
        },
        "oneOf": [
            {"$ref": "#/$defs/" + name}
            for name in ("create", "cancel", "status", "capability", "error")
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    encoded = json.dumps(output_wire_schema(), ensure_ascii=False, indent=2) + "\n"
    path = ROOT / "governance" / "contracts" / NAME
    if args.write:
        path.write_text(encoded, encoding="utf-8", newline="\n")
    if args.check or not args.write:
        if not path.is_file() or path.read_text(encoding="utf-8") != encoded:
            print("authoring output schema differs")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
