#!/usr/bin/env python3
"""Deterministic, network-free M10-03 model-manifest fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core import (  # noqa: E402
    EvidenceLevel,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    ModelBackendFamily,
    ModelCapability,
    ModelCapabilityState,
    ModelManifest,
    ModelProbeStatus,
    ModelRuntimeProfile,
    OllamaModelObservation,
    OllamaServerObservation,
    qualify_ollama_manifest,
)


def _fingerprint(letter: str) -> str:
    return "sha256:" + letter * 64


def _runtime() -> ModelRuntimeProfile:
    return ModelRuntimeProfile(
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
        dtype="float16",
        offload="host_managed",
        max_context_tokens=8192,
        max_output_tokens=512,
        max_media_items=8,
        limits=LocalResourceBudget(2_000_000, 10.0, 8, 32_000, 2, 1),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    capabilities = frozenset(
        {ModelCapability.TEXT_GENERATION, ModelCapability.VISION, ModelCapability.STRUCTURED_OUTPUT}
    )
    native = ModelManifest(
        "native.qwen3_vl_8b",
        ModelBackendFamily.COMFYUI_NATIVE,
        "comfyui_native",
        "1.0.0",
        "qwen3-vl-8b",
        _fingerprint("a"),
        _fingerprint("b"),
        "qwen3_vl_8b",
        "qwen_vl",
        "qwen3_vl",
        True,
        capabilities,
        "h3.model.typed_output.v1",
        "json_object_v1",
        _runtime(),
        ModelCapabilityState.UNQUALIFIED,
        "apache-2.0",
        "comfyui.c44dea",
        EvidenceLevel.EXPERIMENTAL,
        True,
        "fixture-qualified only",
    )
    ollama = ModelManifest(
        "ollama.qwen3_vl_8b",
        ModelBackendFamily.OLLAMA,
        "ollama",
        "1.0.0",
        "qwen3-vl:8b",
        _fingerprint("d"),
        _fingerprint("e"),
        "qwen3_vl",
        None,
        "ollama_native",
        True,
        capabilities,
        "h3.model.typed_output.v1",
        "json_object_v1",
        _runtime(),
        ModelCapabilityState.UNQUALIFIED,
        "apache-2.0",
        "ollama.loopback",
        EvidenceLevel.EXPERIMENTAL,
        True,
        "fixture-qualified only",
        server_version="0.9.0",
    )
    report = qualify_ollama_manifest(
        ollama,
        server=OllamaServerObservation("0.9.0", cloud_enabled=False),
        model=OllamaModelObservation(
            "qwen3-vl:8b", _fingerprint("d"), 1024, "qwen3", ("text_generation", "vision")
        ),
    )
    payload = {
        "schema": "h3.model.fixture.v1",
        "status": "PASS" if report.status is ModelProbeStatus.SUPPORTED else "FAIL",
        "native_manifest_fingerprint": native.fingerprint,
        "ollama_manifest_fingerprint": ollama.fingerprint,
        "ollama_probe": report.to_public_dict(),
        "preferred_route": "comfyui_native",
        "fallback_route": "ollama_explicit_only",
        "automatic_fallback": False,
        "network": "disabled",
        "host_runtime": "not_started",
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
