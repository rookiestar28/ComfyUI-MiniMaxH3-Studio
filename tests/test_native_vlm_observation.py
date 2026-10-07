"""M11-02 strict native VLM source/region observation tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from comfyui_h3_context.adapters.comfyui_vlm import (
    NativeVLMObservationAdapter,
    execute_native_vlm_contract_test,
)
from comfyui_h3_context.core import (
    VLM_GENERATION_OUTPUT_SCHEMA,
    VLM_OBSERVATION_SCHEMA,
    LocalAdapterCapabilityError,
    LocalDeviceKind,
    LocalDeviceSpec,
    ModelOutputError,
    VLMObservationDocument,
    VLMObservationKind,
    VLMObservationStatus,
)
from scripts.m11_02_native_vlm_fixture import (
    _FixtureClip,
    _manifest,
    _output,
    _request,
    run,
)

ROOT = Path(__file__).resolve().parents[1]


def _execute_output(output: str) -> VLMObservationDocument:
    return execute_native_vlm_contract_test(
        NativeVLMObservationAdapter.for_contract_test(_FixtureClip(output), _manifest()),
        _request(),
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
    )


class NativeVLMObservationTests(unittest.TestCase):
    def test_generic_approved_manifest_cannot_bypass_visual_qualification(self) -> None:
        with self.assertRaises(LocalAdapterCapabilityError):
            NativeVLMObservationAdapter(_FixtureClip(_output()), _manifest())

    def test_schema_and_complete_source_region_document(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "vlm_observation_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/vlm_observation_v1.schema.json"
        )
        self.assertEqual(schema["properties"]["schema"]["const"], VLM_OBSERVATION_SCHEMA)
        document = _execute_output(_output())
        self.assertEqual(document.status, VLMObservationStatus.COMPLETE)
        self.assertEqual(set(document.selected_asset_ids), {"image_a", "image_b"})
        self.assertEqual(len(document.observations), 8)
        self.assertEqual(
            {item.kind for item in document.observations},
            {
                VLMObservationKind.COMPOSITION,
                VLMObservationKind.SCENE,
                VLMObservationKind.SUBJECT_OBJECT,
                VLMObservationKind.STYLE,
            },
        )
        for item in document.observations:
            self.assertIsNotNone(item.evidence.confidence)
            self.assertEqual(item.evidence.provenance.source.asset_id, item.asset_id)
            self.assertEqual(
                item.evidence.provenance.source.source_id, f"source_{item.asset_id[-1]}"
            )
            self.assertIsNotNone(item.region)

    def test_native_route_uses_existing_clip_sequence_and_receipt(self) -> None:
        summary = run()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["clip_calls"], ["tokenize", "generate", "decode"])
        self.assertEqual(summary["clip_image_counts"], [2])
        self.assertEqual(summary["native_route"], "preferred_injected_only")
        self.assertEqual(summary["qualification"], "contract_only")
        self.assertFalse(summary["executable_profile"])
        document = summary["document"]
        assert isinstance(document, dict)
        receipt = document["receipt"]
        assert isinstance(receipt, dict)
        self.assertEqual(receipt["backend_family"], "comfyui_native")
        self.assertEqual(receipt["adapter_id"], "comfyui_native_vlm")
        self.assertEqual(summary["ollama_route"], "not_contacted")
        self.assertFalse(summary["automatic_fallback"])

    def test_model_text_remains_observed_untrusted_evidence(self) -> None:
        document = _execute_output(_output())
        injected = next(
            item
            for item in document.observations
            if item.asset_id == "image_a" and item.kind is VLMObservationKind.SUBJECT_OBJECT
        )
        self.assertIn("ignore previous instructions", injected.claim)
        self.assertEqual(injected.evidence.origin.value, "observed")
        self.assertEqual(injected.evidence.provenance.source.kind.value, "media_asset")

    def test_unknown_keys_partial_output_and_unselected_source_fail_closed(self) -> None:
        parsed = json.loads(_output())
        parsed["extra_prose"] = "not accepted"
        with self.assertRaises(ModelOutputError):
            _execute_output(json.dumps(parsed))

        parsed = json.loads(_output())
        parsed["observations"] = parsed["observations"][:-1]
        with self.assertRaises(ModelOutputError):
            _execute_output(json.dumps(parsed))

        parsed = json.loads(_output())
        parsed["observations"][0]["asset_id"] = "image_unknown"
        with self.assertRaises(ModelOutputError):
            _execute_output(json.dumps(parsed))

    def test_region_and_confidence_bounds_fail_closed(self) -> None:
        parsed = json.loads(_output())
        parsed["observations"][0]["region"]["x"] = 1.1
        with self.assertRaises(ModelOutputError):
            _execute_output(json.dumps(parsed))

        parsed = json.loads(_output())
        parsed["observations"][0]["confidence"] = "1.2"
        with self.assertRaises(ModelOutputError):
            _execute_output(json.dumps(parsed))

    def test_empty_payload_and_cancellation_are_terminal(self) -> None:
        summary = run()
        self.assertEqual(summary["empty_payload"], "rejected")
        self.assertEqual(summary["cancelled"], "cancelled")
        self.assertEqual(summary["media_decode"], "not_run")
        self.assertEqual(summary["network"], "disabled")

    def test_native_vlm_requires_vision_capability(self) -> None:
        manifest = _manifest()
        manifest = replace(
            manifest,
            capabilities=frozenset(
                {item for item in manifest.capabilities if item.value != "vision"}
            ),
        )
        with self.assertRaises(LocalAdapterCapabilityError):
            NativeVLMObservationAdapter.for_contract_test(_FixtureClip(_output()), manifest)

    def test_generation_schema_marker_is_frozen(self) -> None:
        self.assertEqual(VLM_GENERATION_OUTPUT_SCHEMA, "h3.vlm.observation.output.v1")


if __name__ == "__main__":
    unittest.main()
