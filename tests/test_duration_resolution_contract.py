"""The generated browser duration domain stays bound to the Python backend authorities."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from comfyui_h3_context.adapters.comfyui_duration_resolution import (
    DURATION_RESOLUTION_REQUEST_SCHEMA,
    resolve_duration_request,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "duration_resolution_contract.py"
GENERATED = ROOT / "frontend" / "src" / "contracts" / "generatedDurationResolution.ts"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("duration_resolution_contract_generator", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generator_check_passes_for_the_committed_browser_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_generated_row_is_the_exact_backend_route_response() -> None:
    rows = _generator().canonical_rows()
    assert [row["requested_seconds"] for row in rows] == list(range(4, 16))
    assert rows == tuple(
        resolve_duration_request(
            {
                "schema": DURATION_RESOLUTION_REQUEST_SCHEMA,
                "requested_seconds": requested_seconds,
            }
        )
        for requested_seconds in range(4, 16)
    )
    assert rows[-1] == {
        "schema": "h3.context.duration_resolution.v1",
        "requested_seconds": 15,
        "requested_milliseconds": 15000,
        "effective_milliseconds": 15083,
        "frame_count": 362,
        "snapped": True,
    }


def test_generated_module_contains_only_closed_local_contract_data() -> None:
    text = GENERATED.read_text(encoding="utf-8")
    assert "canonicalDurationResolutions" in text
    assert "Math.round" not in text
    assert "http://" not in text
    assert "https://" not in text
